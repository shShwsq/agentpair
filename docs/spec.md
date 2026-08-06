# AgentPair 双智能体协作系统 - 规格说明

## 1. 概述

### 1.1 产品定位
双智能体协作的代码分析平台,核心创新是 **user_agent 模拟用户追问** 的交互模式,在单 ReAct 架构之上叠加意图对齐与结果审视能力。

**场景降级后的定位变更**:系统不再绑定安全审计场景。checklist 由 user_agent 动态生成 + 用户编辑确认,prompt 通用化,工具全部开放,结果结构通用化。安全审计仅作为预设场景模板(快捷提示词 + 推荐 skill)之一,另含代码审查等场景。

### 1.2 核心架构
```
用户输入(目的/仓库URL)
      ↓
┌─────────────┐    提问澄清     ┌──────────────┐
│ user_agent  │ ←----------→ │   用户(人)    │
│ (意图对齐+   │               └──────────────┘
│  结果审视)   │
└──────┬──────┘
       │ 定向追问请求
       ↓
┌──────────────────────────────┐
│   ExecutorAgent (抽象层)      │
│  ┌─────────┐  ┌─────────────┐ │
│  │ builtin │  │ 外部CLI(via │ │  工具调用 + 推理 + 执行
│  │ react_  │  │ ACP 协议)   │ │
│  │ agent   │  │ (Qoder CLI) │ │
│  └─────────┘  └─────────────┘ │
└──────┬───────────────────────┘
       │ 执行结果
       ↓
┌─────────────┐
│ user_agent  │ 对照 checklist 评估覆盖度与深度
│ (审视+追问)  │ → 满足则输出给用户 / 不满足则继续追问 react_agent
└─────────────┘
```

**执行器抽象层(ExecutorAgent)**:把"执行智能体"抽象为统一接口,支持多种实现:
- `builtin`:系统内置 ReAct 智能体(基于 react_agent.py),使用后端配置的 LLM
- `qoder_cli` / `qoder_cli_cn`:沙箱内运行 Qoder CLI(国际版 / 国内版),通过 ACP 协议通信,模型由 CLI 账号配额管理
- `kimi_cli`:沙箱内运行 Kimi Code CLI(开源),通过 ACP 协议通信,模型经 `KIMI_MODEL_*` 环境变量注入(支持自部署 LLM 端点)
- 扩展性:新增 agent 类型只需在 registry 注册,无需改核心代码

### 1.3 双端策略
- **网站端**:完整功能,适合深度审计报告查看与导出
- **微信小程序端**:核心功能,适合快速扫描与移动端查看
- **后端共用**:双智能体核心逻辑作为后端服务,两端通过统一 API 访问

---

## 2. 场景定义

### 2.1 场景降级说明
场景已从"硬编码全流程"降级为**快捷模板**。场景模板仅提供:
- `preset_prompt`:预设提示词(用户选场景后预填到输入框)
- `recommended_skills`:推荐技能列表(创建任务时默认勾选)

不再承担:checklist(改为动态生成)、prompt(改为通用)、工具白名单(改为全部开放)、结果 schema(改为通用化)。

### 2.2 当前支持场景
**场景一:代码安全审计**(`code_security_audit`)
- 预设提示词:关注注入类、认证授权、反序列化、SSRF、配置泄露、XSS、路径穿越等
- 推荐 skill:check_sql_injection / check_hardcoded_secrets / check_ssrf

**场景二:代码审查**(`code_review`)
- 预设提示词:关注代码质量、可读性、正确性、性能等

**场景三:通用**(`general`)
- 无预设提示词,用户自行描述任务

### 2.3 场景扩展预留
用户在前端选择场景,后端加载对应预设提示词与推荐 skill。checklist 由 user_agent 动态生成,不依赖场景定义。

---

## 3. 核心流程:双智能体协作

### 3.1 user_agent 阶段一:意图对齐(可选)
触发条件:用户输入模糊或缺少关键信息(如未指定分支、任务范围不清)。
- 第 0 轮初始评估时,user_agent 可输出 `ask_user=true` + questions 列表向用户提问
- **预算上限 2 轮**(避免无限澄清),超过后强制关闭提问
- questions 支持选择题(choice)和填空题(text),系统自动追加"是否有其他补充"问题
- 前端弹窗交互,用户提交后后台线程唤醒继续评估
- 典型问题:
  - "请确认范围:全仓库还是特定目录?"
  - "有特定关注的类别吗?"
  - "是否需要检查依赖项漏洞(CVE)?"

如果用户输入已明确,跳过此阶段直接进入 3.2。

### 3.2 react_agent 阶段:首轮扫描
user_agent 将用户意图转化为任务,交给执行器(ExecutorAgent)。
- 执行器可以是内置 react_agent(ReAct 模式)或外部 CLI(Qoder CLI via ACP)
- 内置 react_agent 拥有工具:clone_repo / list_files / find_files / read_file / search_code / run_semgrep / query_cve / write_file / run_python_code / list_skills / skill
- orchestrator 预处理:若用户选了仓库,主动 clone + list_files,仓库结构注入第 0 轮 user_agent 和第 1 轮 react_agent
- 输出首轮自然语言总结(summary)

### 3.3 user_agent 阶段二:对照 checklist 评估
user_agent 对照**动态生成的 checklist**评估每轮:
- **第 0 轮**:user_agent 根据用户意图动态生成 checklist(3-8 个维度,每个含子项),推送给用户编辑确认后落库
- **协作轮**(第 1 轮起):从 task.checklist 读取已确认的清单,评估:
  - 维度覆盖度:哪些维度已查、哪些未触及
  - 维度深度:已查维度是否触及必查子项
  - 已知发现的交叉验证:是否存在矛盾或需要补强的结论
- 跨轮记忆:user_agent 注入自己之前各轮的评估记录,避免 covered/missing 反复摇摆

### 3.4 漏洞类别 checklist(场景降级后:动态生成)
**场景降级变更**:checklist 不再从场景固定读取,改为 user_agent 第 0 轮根据用户意图动态生成。

**动态生成原则**:
- 根据用户意图自适应:安全审计任务生成安全维度(注入/认证/反序列化等),代码审查任务生成质量维度(可读性/正确性/性能等),其他任务按语义生成
- 3-8 个维度为宜,每个维度含 3-6 个子项(checklist)
- 维度 id 用英文下划线命名(如 injection / readability),name 用中文
- 用户可编辑确认后落库到 task.checklist,后续协作轮按此评估

以下为安全审计场景的**参考维度**(user_agent 生成时可能调整):

| 维度 | 必查子项(示例) | 高风险语言 |
|------|------------------|------------|
| 注入(SQL/Cmd/模板) | 用户输入拼接、ORM 原始查询、shell 调用 | 全部 |
| 认证与授权 | 登录流程、会话管理、IDOR、权限校验 | 全部 |
| 反序列化 | pickle/yaml/marshal/eval | Python、Java |
| SSRF | 外部 URL 请求、回调、元数据接口 | 全部 |
| 硬编码密钥 | API key、token、password、私钥 | 全部 |
| 路径穿越 | 文件操作拼接、zip 解压 | 全部 |
| 不安全加密 | 弱算法、ECB 模式、硬编码 IV | 全部 |
| XSS | 模板转义、DOM 操作、CSP | Web 应用 |
| 配置安全 | 调试模式、CORS、默认凭据 | 全部 |

> 注:此为参考 checklist,实际由 user_agent 动态生成 + 用户编辑确认。

### 3.5 user_agent 阶段三:定向追问
当 checklist 未覆盖或深度不足时,user_agent 向 react_agent 发送**定向追问**:
- **追问要具体到类别和检查点**,禁止 "你再查查有没有别的" 这类无方向指令
- 追问要带上**已有发现作为上下文**,避免 react_agent 重复扫描
- 示例:"已发现 SQL 注入 2 处(位置见上文)。请继续检查认证与授权模块,重点关注:1) 权限校验是否在每个受保护路由上;2) JWT 验证是否校验签名与过期;3) 是否存在 IDOR(通过用户可控 ID 访问他人资源)。"

### 3.6 终止条件(硬性,避免死循环)
满足以下**全部**条件后输出最终报告:
1. checklist 所有适用维度均有明确结论(有 / 无 / 无法确定)
2. 每个维度至少触及一个必查子项
3. user_agent 追问轮次 ≤ **上限 4 轮**(MAX_ROUNDS,可配置)
4. 最近一轮 react_agent 未产生新发现,且 user_agent 无新增追问点

**完成后重启**:用户在任务完成后追加消息,可触发新一轮协作(resume_audit_with_message),
最多再跑 3 轮(MAX_RESUME_ROUNDS)。重启时不复用旧 plan,让 LLM 根据新消息重新规划。

### 3.7 任务暂停/恢复
用户可暂停运行中的任务:
- 后台线程在检查点(迭代边界 / 工具调用前)阻塞
- `task.status` 变为 `paused`,前端展示暂停状态
- 恢复后从阻塞点继续执行

### 3.8 用户补充消息
用户可在对话界面下方输入框发送补充消息:
- **运行中/暂停中**:消息入队,react_agent 下一迭代边界 drain 出来注入 LLM 上下文
- **完成后**:启动新的协作 round,先调 user_agent 分析消息,再决定是否触发新一轮 react_agent
- 消息统一落库为 Conversation(role=user, type=message) + 推送 SSE

---

## 4. 双端实现策略

### 4.1 共用后端(推荐)
- 后端服务暴露统一 REST API + SSE(Server-Sent Events)实时流
- 核心双智能体逻辑、checklist、工具集均在后端
- 双端只负责 UI 与交互

### 4.2 前端框架选择(已决策)
- **网站前端**:Vue3 + TypeScript(已实现)
- **小程序前端**:微信小程序原生开发(未实现)

### 4.3 长任务处理
代码分析是长任务(几分钟到几十分钟):
- **网站端**:SSE(Server-Sent Events)实时推送进度,包含:
  - `conversation`:对话消息(user_agent / react_agent 的每一步)
  - `status`:任务状态变更(进入新阶段)
  - `thinking_delta`:LLM 流式 token 增量(打字机效果)
  - `question`:用户澄清提问(前端弹窗)
  - `checklist_review`:覆盖度清单确认
  - `plan`:计划清单状态更新
  - `done` / `error`:终止事件
- **小程序端**:异步处理,**不主动通知**,用户自行进入小程序查看进度与结果
  - 用户提交后立即返回任务 ID
  - 后端异步执行,任务列表持久化
  - 任务列表页展示状态(pending / running / paused / completed / failed),用户过几分钟自行刷新查看
  - running 状态下可展示当前阶段(如"正在克隆仓库"、"user_agent 评估中"、"react_agent 第 3 轮扫描")
  - 前端定时轮询任务状态(建议 5-10 秒间隔,带退避)

---

## 5. 数据模型(核心实体)

```
Task(任务)
  - id (UUID)
  - user_id (UUID, 可空:匿名任务)
  - scenario: 场景标识字符串(默认 "general")
  - title: 任务标题(可空,为空时前端用 user_input 截断展示)
  - user_input: 用户原始输入(意图,通用化:不再固定 repo_url)
  - params: JSONB,可选补充参数(repo_url / branch / scope 等)
  - checklist: JSONB,动态覆盖度清单(user_agent 第 0 轮生成 + 用户编辑确认)
  - allowed_skills: JSONB,用户选择的允许调用的 skill 名称列表(空=全部可用)
  - status: pending / running / paused / completed / failed
  - current_stage: 当前阶段描述(展示给前端)
  - error_message: 失败时的错误信息
  - llm_config_id: user_agent 使用的 LLM 配置 ID
  - react_llm_config_id: 内置 react_agent 使用的 LLM 配置 ID(空=回退到 llm_config_id)
  - executor: 执行器选择("builtin" / "qoder_cli" / "qoder_cli_cn")
  - created_at, completed_at

Conversation(对话)
  - id (UUID)
  - task_id
  - round_idx: 协作轮次(0=初始评估,1+=协作轮)
  - role: user / user_agent / react_agent / system
  - type: question / answer / evaluation / followup / thinking / tool_call / tool_result / summary / error / message / question(澄清提问)
  - content: 消息内容
  - reasoning: 思考链(仅 type=thinking 有,模型 reasoning_content)
  - created_at

Result(任务结果项,通用)
  - id (UUID)
  - task_id
  - round_idx: 由第几轮 react_agent 产出
  - title: 结果标题
  - content: 结果详细内容
  - metadata: JSONB,场景专用信息(安全场景: cwe/severity/file_path/line_range 等)
  - created_at
```

**场景降级变更**:
- Task 从固定 `repo_url/branch/scope` 字段改为 `user_input`(通用意图)+ `params`(JSONB 补充参数)
- Finding 表改为通用的 Result 表,metadata 放场景专用字段
- Task 新增 `checklist`(动态覆盖度清单)、`allowed_skills`(技能过滤)、`executor`(执行器选择)、`react_llm_config_id`(react_agent 独立模型配置)
- Task 新增 `paused` 状态
- Conversation 新增 `round_idx`(协作轮次)、`reasoning`(思考链)、`message`(用户补充消息)、`history_compress`(LLM 压缩缓存)等类型

---

## 6. 报告输出

### 6.1 报告结构
- 执行摘要(总览、结果分布、覆盖维度)
- 结果清单(按分组维度排序,含位置与详情)
- 覆盖度说明(查了哪些维度、结论如何)
- 附录:完整对话记录(可选展开)

### 6.2 双端差异
- 网站:完整报告 + 导出 PDF/Markdown
- 小程序:精简视图(摘要 + 漏洞卡片),详细报告可跳转网页版

---

## 7. 安全与合规

### 7.1 仓库访问
- 支持公开仓库(默认)
- 私有仓库已支持(用户绑定 GitHub / Gitee OAuth 后,clone 时用对应平台的 access_token 访问)
- **多平台抽象**:统一 `GitProvider` 抽象层(GitHub / Gitee),按仓库 URL 主机自动识别平台并选用对应 token
- clone 协议回退:HTTPS+token → SSH → HTTPS 匿名
- token 注入格式按平台差异:GitHub 用 `x-access-token:{token}@github.com`,Gitee 用 `oauth2:{token}@gitee.com`

### 7.2 微信内容安全
- 审计报告中可能含敏感词(如 "漏洞"、代码片段中的关键字)
- 输出前需过微信内容安全检测,避免被拦截

### 7.3 资源限制
- 单任务执行时间上限(防 react_agent 死循环)
- 单任务 token 消耗上限
- 仓库克隆大小限制

---

## 8. 待决策问题

### 8.1 前端框架与后端栈(已决策)
**方案:双端独立开发。**

- **网站前端**:Vue3 + TypeScript ✅ 已实现
- **小程序前端**:微信小程序原生开发 ⬜ 未实现
- **后端**:Python + FastAPI ✅ 已实现
- **理由**:网站要做深度报告展示与复杂交互,小程序只做核心功能与移动端查看,各自独立反而更省心,避免跨端框架的限制与调试成本

### 8.2 小程序长任务通知机制(已决策)
**方案 C:用户主动进入查看,不做主动通知。**
- 用户提交任务后立即返回任务 ID
- 用户自行进入小程序任务列表查看状态
- running 状态展示当前阶段,辅助用户判断还要等多久
- 后续如需增强体验,可考虑订阅消息作为增量功能,不作为首版必需

### 8.3 LLM 模型选择(已决策)
**方案:用户可自主配置 LLM,管理员预置厂商清单。**

- **厂商清单**(models_catalog.json):描述各厂商差异(thinking 参数名、思考模式、温度等),已支持:
  - DashScope(通义千问)、DeepSeek、智谱(ZhipuAI)、Kimi、豆包、MiniMax 等
  - 任何 OpenAI 兼容接口的厂商
- **用户配置**(UserLLMConfig):列表式配置,每个配置含 provider / api_key / model / enable_thinking / base_url
- **任务级选择**:任务提交时选 `llm_config_id`(user_agent 用)+ `react_llm_config_id`(react_agent 用,空=回退到 llm_config_id)
- **分离配置**:user_agent 与 react_agent 可选不同模型(react_agent 工具调用重,倾向更强模型;user_agent 偏评估与追问,可用较小模型降本)
- **思考链**:支持 DeepSeek-R1 / Qwen-QwQ / Kimi-k2.6 等模型的 reasoning_content,通过 SSE 实时推送给前端
- **外部 CLI 执行器**:Qoder CLI / Qoder CN CLI 的模型由 CLI 账号配额管理;Kimi Code CLI 的模型经 `KIMI_MODEL_*` 环境变量注入(支持自部署 LLM 端点)。均不走后端 LLM 配置

### 8.4 react_agent 工具集(已决策)
**首版工具集范围(全部已实现):**

| 工具 | 用途 | 实现 |
|------|------|------|
| clone_repo | 克隆 Git 仓库到沙箱(支持 GitHub / Gitee,HTTPS+token / SSH / 匿名) | ✓ |
| list_files | 列出目录结构(单层,跳过 .git/node_modules 等噪声目录) | ✓ |
| find_files | 按文件名 glob 模式递归查找文件(如 **/*.py) | ✓ |
| read_file | 读取文件内容(带行号,支持 offset 翻页) | ✓ |
| search_code | 正则搜索代码,支持 content/files_with_matches/count 三种输出模式 | ✓ |
| run_semgrep | 在沙箱里跑 Semgrep 静态分析(仅 sandbox 模式可用) | ✓ |
| query_cve | 解析依赖文件 + 查 CVE 数据库(OSV API,按依赖逐个查) | ✓ |
| write_file | 在工作区写文件(PoC 脚本、补丁、报告等) | ✓ |
| run_python_code | 在沙箱执行 Python 代码(验证 PoC / 跑分析脚本 / 执行测试) | ✓ |
| list_skills / skill | 查看并加载专家技能(获取 SKILL.md 指令后按其指引执行) | ✓ |

**场景降级后**:工具全部开放,不再按场景过滤。用户创建任务时可通过 `allowed_skills` 选择允许调用的 skill。

### 8.5 用户账户体系(已决策)
**方案:邮箱密码注册登录 + Git 平台 OAuth 登录(GitHub / Gitee),均已实现。**

- **登录方式**:
  - 邮箱 + 密码(需先验证邮箱)
  - GitHub OAuth 登录(自动注册/关联/登录)
  - Gitee OAuth 登录(自动注册/关联/登录)
- **注册流程**:邮箱 + 密码 + 邮箱验证(验证后才激活账号)
- **两端统一账户**:同一套账户体系,网站和小程序共用

**密码策略(已实现)**:
- **允许「修改密码」**:已登录用户通过 `/auth/password/change` 修改密码,需验证当前密码(密码用户可直接设新密码)。新密码不能与当前密码相同
- **允许「重置密码」**:未登录状态下,通过邮箱验证发起重置。用户通过邮箱验证身份后设置新密码

**忘记密码流程**:
1. 用户在登录页点「忘记密码」,输入注册邮箱
2. 后端发送重置链接到邮箱(链接含一次性 token,有效期 30 分钟)
3. 用户点击链接验证 token 后,设置新密码
4. 新密码生效,旧密码失效

**Git 平台 OAuth 登录(已实现,GitHub / Gitee)**:
- 作为独立登录方式(与邮箱密码并列),前端有 GitHub / Gitee 登录按钮 + 各自 OAuth 回调页
- 登录逻辑(两平台一致):
  - 该平台已绑定 → 直接登录
  - 该平台未绑定但 email 已注册 → 关联该平台后登录
  - 完全新用户 → 自动创建账号(无密码,平台邮箱隐含验证;仅 GitHub 支持可验证邮箱同步)
- **统一抽象层**:`GitProvider` ABC(`GitHubProvider` / `GiteeProvider`),封装各平台 OAuth 端点、token 交换、用户信息、仓库列表、URL 转换差异
- **多平台绑定**:同一用户可同时绑定 GitHub + Gitee,任务克隆时按仓库 URL 主机自动选用对应平台 token
- **私有仓库访问**:绑定平台后,clone 私有仓库时用对应平台 access_token 访问(token 注入格式按平台差异:GitHub `x-access-token:{token}@`,Gitee `oauth2:{token}@`)
- **数据模型**:`UserGitBinding` 表(provider / provider_user_id / 加密的 access_token),取代旧的 `User.github_id` / `github_access_token` 字段;启动时一次性迁移旧数据
- **邮箱同步**:仅 GitHub 支持(verified primary email 视为已验证);Gitee 不支持可验证邮箱,绑定时不触发邮箱同步弹窗

**删除账号(已实现)**:
- 用户在设置页发起删除,需输入完整邮箱二次确认
- 硬删除:连带删除 task / Conversation / Result / UserLLMConfig / EmailToken / UserGitBinding(均 `ondelete=CASCADE`)
- Git 平台关联解除(provider_user_id 释放,可被其他账号绑定)

**安全考虑**:
- 密码存储:bcrypt 加盐哈希
- 登录失败限制:同 IP 同邮箱连续失败 5 次锁定 15 分钟
- 邮箱验证 token:一次性,使用后失效
- 重置链接 token:一次性,有效期 30 分钟
- JWT:access token + refresh token 双 token 机制

### 8.6 沙箱与自定义技能机制(已决策)

**沙箱:OpenSandbox(已实现)**

- 用于隔离执行不可信代码,基于 Docker 的隔离机制
- **双模式支持**:
  - `sandbox` 模式:连真实 OpenSandbox Server(部署在 Linux 服务器),走 SandboxSync 同步 API
  - `mock` 模式:本地未部署 Server 时用本地文件系统模拟,供开发期使用
  - 通过 `SANDBOX_MODE` 环境变量切换
- SandboxSession 统一接口(两种模式行为一致):
  - `run_command(cmd)` → stdout(同步执行)
  - `run_command_background(cmd)` → execution_id(非阻塞,用于启动 ACP bridge 等长驻服务)
  - `get_background_logs(execution_id)` → 累积日志
  - `write_file` / `read_file` / `get_endpoint(port)` / `close()`
- 三类执行场景:
  1. **跑仓库代码本身**:沙箱预装多语言 runtime(Python/Node/Go 等),用于跑用户仓库的测试或启动服务观察运行时行为
  2. **跑 agent 生成的验证脚本**:react_agent 写小脚本验证漏洞是否可利用(如构造 payload 测注入点),在沙箱执行取回结果
  3. **跑第三方 SAST 工具**:Semgrep 等工具在沙箱里执行扫描仓库
- 安全配置(沿用 OpenSandbox 的安全实践):
  - security_opt、资源限制(CPU/内存/执行时间)
  - 只读文件系统 + 指定可写目录
  - 网络隔离(默认无外网,需要时按需开放)
  - 执行时间硬上限(防死循环)
  - 可选 SSH key 挂载卷(供 git clone git@github.com:... 使用)

**自定义技能机制(SKILL,已实现)**

- **定位**:把常用的多步审计操作封装成可复用的「技能」,react_agent 按需调用
- **与普通工具的区别**:普通工具是单步操作(clone、read_file、run_command),技能是 Markdown 指令(LLM 读后自行编排多步)
- **技能定义格式:SKILL.md**(Markdown frontmatter + body,非 YAML 步骤编排):
  ```markdown
  ---
  name: check_sql_injection
  description: 检查 SQL 注入漏洞,覆盖拼接、ORM raw 查询、动态表名等模式。
  ---

  # SQL 注入审计

  ## 执行步骤
  ### 1. 定位数据库交互点
  调用 `search_code` 搜以下高危模式……
  ### 2. 判断是否用户可控
  对每个搜索命中点,调用 `read_file` 看上下文……
  ### 3. 验证(可选,沙箱可用时)
  ### 4. 提交结果
  调用 `submit_results` 提交……
  ```
- **设计说明**:skill 不是硬编码步骤编排,而是给 LLM 的自然语言指令。LLM 读取 body 后自行决定调用哪些工具、按什么顺序执行,灵活性远高于固定 YAML 步骤
- **目录结构**:`<skills_root>/<scenario_id>/<skill_name>/SKILL.md`(skill 目录可含附加资源文件)
- **加载机制**:进程启动时扫描磁盘所有 SKILL.md,解析 frontmatter,注册到 SkillRegistry。管理员后台增删后调 `reload_registry()` 刷新
- **管理 API**:`GET/POST/DELETE /skills`(CRUD)+ `POST /skills/reload`(重新扫描)
- **react_agent 调用**:通过 `list_skills` 工具查看可用 skill,通过 `skill` 工具加载指定 skill 的 body 到上下文,LLM 按其指引执行
- **首版技能清单**(场景降级后按 scenario 组织,用户创建任务时可选 `allowed_skills` 过滤):
  - `code_security_audit/check_sql_injection`(注入类)
  - `code_security_audit/check_hardcoded_secrets`(硬编码密钥)
  - `code_security_audit/check_ssrf`(SSRF)
- **扩展性**:管理员可通过 API 或直接编辑磁盘文件添加新 skill,用户不能自定义(避免安全风险)

### 8.7 人工审计师模式(后期功能,首版不实现)

**定位**:当用户未配置模型时,可选「人工模式」,由其他真人开发者扮演 agent 完成审计。首版不做,记录在此供后续参考。

**触发场景**:
- 用户没有配置自己的 LLM 模型(或不想消耗自己的额度)
- 用户主动选择「人工模式」,接受较长等待时间

**任务模式三选一(规划)**:
- A. 自动模式:LLM 双 agent 执行(默认)
- B. 人工模式:由真人审计师接单完成,等待时间长
- C. 混合模式(可能后期加入):LLM 先跑,真人审计师复核与补充

**激励机制:额度置换(关键设计)**

- 接单帮别人审计 → 获得额度
- 额度可用于:调用管理员预置的 LLM 模型跑自己的任务
- 形成正向循环:帮别人审 → 赚额度 → 自己任务用 LLM 跑
- 管理员预置模型作为公共资源池,所有用户共享
- 不涉及真实金钱流动(避免支付合规与微信虚拟支付管控问题)

**防滥用**:
- 单次接单获得的额度有上限
- 接单质量评估机制(提问者评分)
- 接单后未完成的惩罚(扣额度或限制接单)
- 管理员预置模型的总消耗成本需可控(避免一人帮审水任务换大量 LLM 调用)

**关键设计问题(待后续细化)**:
- 供需匹配机制:谁接哪个任务?先到先得 vs 匹配?
- 隐私:用户提交的代码愿意让其他用户看吗?是否需要遮蔽敏感信息?
- 质量:真人水平参差,如何保证审计质量?
- 责任:漏报出事谁负责?
- 任务超时:接单后多久未完成算放弃?

**前置条件**:
- 用户量达到一定规模(冷启动期人工池为空,功能无意义)
- 双 LLM agent 模式已稳定运行,有对比基线

---

## 9. 超出原规划的已实现功能

以下功能在原 spec 中未规划,但在开发过程中根据实际需求已实现:

### 9.1 执行器抽象层(ExecutorAgent)

将「执行智能体」抽象为统一接口(`ExecutorAgent` 抽象基类),支持多种实现:
- **BuiltinReactAgent**:系统内置 ReAct 智能体,委托 `react_agent.run_react_agent`,使用后端配置的 LLM
- **ExternalCLIAgent**:基于外部智能体 CLI 的通用包装,通过 registry 声明的 `executor_module` / `executor_func` 延迟加载
- **工厂模式**:`get_executor(task)` 根据 `task.executor` 字段返回对应实例,未知值回退 builtin
- **注册表**(registry):`AGENT_REGISTRY` 含 `qoder_cli`、`qoder_cli_cn`、`kimi_cli`,声明 agent 类型、沙箱镜像、executor 位置等
- 新增 agent 类型只需在 registry 注册,无需改核心代码

### 9.2 ACP Bridge(HTTP ↔ stdio 桥接)

外部 CLI 执行器(如 Qoder CLI)通过 ACP(Agent Communication Protocol)协议通信:
- 沙箱内启动 CLI 进作为长驻服务(`run_command_background`)
- ACP Bridge 作为 HTTP ↔ stdio 桥:后端发 HTTP 请求 → bridge 转 stdio 写入 CLI 进程 → 读取 stdout 返回
- Bridge 支持流式响应(SSE):将 CLI 的 streaming 输出逐 chunk 转发
- 事件翻译:将外部 CLI 的 ACP 事件映射为系统内部 SSE 事件(conversation / thinking_delta / tool_call 等)
- 凭证注入:从 `user_agent_configs` 加载用户保存的 CLI token,注入沙箱环境变量

### 9.3 跨轮记忆三级压缩

react_agent 在多轮 ReAct 迭代中,LLM 上下文会越来越长,采用三级压缩策略:
1. **完整保留**(近期轮次):最近几轮的完整 thinking + tool_call + tool_result 原样保留
2. **丢弃工具详情**(中期轮次):保留 summary 但丢弃 tool_result 原始输出,只留摘要
3. **LLM 压缩**(早期轮次):调 LLM 将早期对话压缩为一段自然语言摘要,存为 `Conversation(type=history_compress)`

压缩后注入下一轮 LLM 上下文,避免 token 爆炸。

### 9.4 循环检测

react_agent 内置循环检测机制,防止 LLM 陷入重复调用:
- **连续相同调用检测**:滑动窗口内连续 N 次相同 tool_call + 相同 arguments → 强制终止
- **交替循环检测**:检测 A→B→A→B 模式的交替循环 → 强制终止
- 检测到循环后推送 SSE 事件,react_agent 输出当前总结并退出

### 9.5 Plan 状态管理

react_agent 维护跨轮 plan 状态:
- 首轮 LLM 生成 plan(任务分解清单),存入 `Conversation(type=plan)`
- 后续轮次注入 `previous_plan`,LLM 可续接未完成项,避免重复规划
- 每轮结束时输出 `final_plan`(可能含已完成/未完成标记),供下一轮继承
- 前端通过 SSE `plan` 事件实时展示计划状态

### 9.6 工作区浏览

前端可浏览已 clone 仓库的文件结构和内容:
- 后端提供 workspace API,列出沙箱内文件树
- 前端树形展示,支持查看文件内容
- 用于用户确认审计范围、理解 react_agent 的分析上下文

### 9.7 SSE 事件体系(完整)

实时流推送的事件类型(完整清单):

| 事件 | 说明 |
|------|------|
| `conversation` | 对话消息(user_agent / react_agent 的每一步) |
| `status` | 任务状态变更(进入新阶段) |
| `thinking_delta` | LLM 流式 token 增量(打字机效果) |
| `question` | 用户澄清提问(前端弹窗) |
| `checklist_review` | 覆盖度清单确认(用户编辑后落库) |
| `plan` | 计划清单状态更新(跨轮续接) |
| `done` | 任务完成 |
| `error` | 任务失败/异常 |

### 9.8 前端路由与页面

网站前端已实现以下页面(Vue Router):

| 路由 | 页面 | 说明 |
|------|------|------|
| `/` | HomeView | 首页/任务列表 |
| `/tasks/new` | TaskCreateView | 创建新任务(选场景/仓库/skill/模型) |
| `/tasks/:id` | TaskDetailView | 任务详情(SSE 实时流 + 对话 + 报告) |
| `/models` | ModelSettingsView | LLM 模型配置(多厂商列表式管理) |
| `/settings` | SettingsView | 用户设置(改密码/Git 平台绑定 GitHub+Gitee/删除账号) |
| `/login` | LoginView | 登录(邮箱密码 + GitHub / Gitee OAuth) |
| `/auth/github/callback` | OAuthCallbackView | GitHub OAuth 回调(登录/绑定共用) |
| `/auth/gitee/callback` | OAuthCallbackView | Gitee OAuth 回调(登录/绑定共用) |
| `/auth/verify-email` | VerifyEmailView | 邮箱验证 |
| `/auth/password/reset` | ResetPasswordView | 重置密码 |

路由守卫:受保护路由未登录跳 `/login?redirect=...`;已登录访问 `/login` 跳首页;页面刷新时自动 `fetchMe` 恢复会话。
