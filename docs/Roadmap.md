# AgentPair 开发 Roadmap

> 适合 vibe coding 的迭代路线:每个阶段都是一个**能独立跑通、能立即看到效果**的闭环。做完一个阶段先玩一下,有手感了再进下一个。

---

## 阶段 0:最小骨架跑通

**目标**:后端能起,数据库能连,一个空的 agent 接口能返回假数据。

**做什么**:
- FastAPI 项目骨架 + 路由分层
- SQLite + SQLAlchemy 模型(User / Task / Conversation / Finding)
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

## 阶段 1:单 react_agent 跑通真实审计

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

## 阶段 2:加沙箱,代码在沙箱里跑

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

---

## 阶段 3:加 Semgrep 和 CVE 查询

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

---

## 阶段 4:引入 user_agent,双智能体协作

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

---

## 阶段 5:SKILL 机制

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

---

## 阶段 6:用户系统与模型配置

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

---

## 阶段 7:网站前端

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

---

## 阶段 8:微信小程序

**目标**:小程序端,核心功能可用。

**做什么**:
- 微信登录(复用网站账户体系)
- 提交任务(输入 repo_url)
- 任务列表 + 状态轮询(5-10 秒)
- 任务详情:精简版漏洞卡片
- 设置页:模型配置
- 跳转网页版看完整报告

**验证方式**:
- 手机上提交任务,过几分钟回来看结果
- running 状态下能看到当前阶段
- 漏洞卡片能看清重点

**完成标志**:小程序端能完整走一遍提交 → 等待 → 查看结果。

---

## 阶段 9:生产化与上线

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
- 跑一个真实开源项目(比如某个中小型 GitHub 项目),看完整审计效果

**完成标志**:线上可访问,真实仓库审计可用。

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
