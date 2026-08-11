# 双智能体架构与上下文传递逻辑

本文档整理 `backend/app/agents/` 目录下 `user_agent`、内置 `react_agent`、外部 CLI agent（Qoder / Kimi / Hermes / Codex）的代码逻辑、协作流程与上下文传递机制。

> 阅读前置：`docs/spec.md`（产品定义）、`backend/app/agents/orchestrator.py`（协作编排）。

---

## 1. 架构总览

系统采用 **user_agent + react_agent 双智能体协作** 模型，由 `orchestrator.run_dual_agent_audit` 编排：

```
用户意图
   │
   ▼
┌───────────────┐  round 0  ┌──────────────────┐
│  user_agent   │ ◄──────►  │  用户(澄清/清单确认)
│  (评估者)     │           └──────────────────┘
└───────┬───────┘
        │ followup_query
        ▼
┌──────────────────────────────────────────────┐
│ ExecutorAgent (按 task.executor 派发)         │
│   ├─ BuiltinReactAgent  → react_agent.py     │
│   └─ ExternalCLIAgent   → acp_base + wrapper │
│      (qoder_cli / kimi_cli / hermes_cli /    │
│       qoder_cli_cn / codex_cli)              │
└───────────────┬──────────────────────────────┘
                │ summary + plan
                ▼
       user_agent 对照 checklist 评估
                │
        ┌───────┴───────┐
   done=true        missing≠∅
        │               │
   落库 results    followup_query → 下一轮
```

### 1.1 角色分工

| 角色 | 职责 | 是否调工具 | 模型来源 |
|------|------|-----------|---------|
| **user_agent** | 评估覆盖度、生成 checklist、追问、整理结构化结果 | 否，只输出 JSON 评估 | `task.llm_config_id` |
| **react_agent（内置）** | ReAct 循环执行代码分析（clone / search / read / semgrep 等） | 是，调用沙箱工具 | `task.react_llm_config_id`（空时回退 `llm_config_id`） |
| **ExternalCLIAgent** | 沙箱内启动外部 CLI，通过 ACP 协议通信 | 是，由 CLI 自主调工具 | CLI 自管（凭证经环境变量注入） |

### 1.2 协作轮次

- **round 0**：user_agent 初始评估
  - 生成动态 checklist（覆盖度维度）+ 初始 `followup_query`
  - 可输出 `ask_user=true` 触发用户澄清弹窗（最多 `MAX_ASKS=2` 轮）
  - 输出 checklist 推送给用户编辑确认，落库到 `task.checklist`
- **round 1..MAX_ROUNDS(=4)**：协作循环
  - react_agent 执行一轮 → 返回 `summary`
  - user_agent 对照 `task.checklist` 评估 → 输出 `covered/missing/followup_query/done`
  - `done=true` 时输出 `results + grouping`，orchestrator 落库
- **resume（完成后重启）**：用户追加消息触发，最多 `MAX_RESUME_ROUNDS=3` 轮

---

## 2. user_agent 详解

**文件**：[backend/app/agents/user_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/user_agent.py)

### 2.1 核心特征

- **不直接调工具**，只输出结构化 JSON 评估
- **场景降级后通用化**：不再从场景读 checklist，第 0 轮动态生成，后续轮从 `task.checklist` 注入
- **流式输出**：`_stream_user_agent_llm` 通过 `client.chat_stream` 收 token，实时推送 `thinking_delta` 事件给前端
- **跨轮记忆**：第 2 轮起注入自己之前各轮的评估记录，避免 covered/missing 反复摇摆

### 2.2 输入参数（`run_user_agent`）

```python
def run_user_agent(
    user_intent: str,                    # 用户原始意图(含仓库地址/分支)
    react_agent_summaries: list[dict],   # 之前各轮 react_agent 的 summary
    task_id, db, round_idx, scenario_id,
    client: LLMClient | None,            # None 时回退 env 默认
    ask_round=0,                         # 第 0 轮提问循环的轮次
    repo_context: str | None,            # 仅 round 0,主动 clone 后的仓库结构
    task_checklist: list[dict] | None,   # 协作轮从 task.checklist 读
    user_id, repo_url,
) -> dict
```

### 2.3 输出结构

```json
{
  "covered": ["dim_id1"],            // 已覆盖维度
  "missing": ["dim_id2"],            // 未覆盖维度
  "reasoning": "评估理由",
  "followup_query": "针对 missing 的追问指令",
  "done": false,
  "ask_user": false,                 // round 0 可 true
  "questions": [...],                // ask_user=true 时必填
  "checklist": [...],                // 仅 round 0 输出
  "results": [...],                  // 仅 done=true 时输出
  "grouping": {"field":..., "values":[...]} | null
}
```

### 2.4 上下文构造

#### System Prompt（`USER_AGENT_SYSTEM_PROMPT`）

固定模板 + `{checklist_section}` 占位符替换：

- **round 0**：`task_checklist=None` → 提示 LLM "本轮尚未有 checklist，请动态生成"
- **round ≥1**：`task_checklist=task.checklist` → 格式化为 "已确认的覆盖度清单" 注入

末尾追加 **长期记忆段**（`build_user_agent_memory_section`）：
- User Profile（用户偏好，自由文本，≤2000 字符）
- 全局长期记忆（`UserMemory.content`，≤2000 字符）
- 当前项目记忆精简版（`Project.memory_summary`，≤2000 字符；user_agent 不在沙箱，无法 read_file 查阅完整版）

#### User Message

**round 0（无 react_agent_summaries）**：
```
用户原始意图：{user_intent}
这是任务开始，react_agent 还没执行。请输出初始评估...
[当前可向用户提问] 这是第 N 次评估，最多可提问 2 次...
[已预克隆仓库结构,供你参考给出初始指令]
{repo_context}
```

**round ≥1（有 react_agent_summaries）**：
```
用户原始意图：{user_intent}

[你之前各轮的评估记录(保持覆盖度判断连续性)]
=== 第 1 轮 user_agent 评估 ===
{history_prefix from _build_user_agent_history}

以下是 react_agent 已执行的 N 轮自然语言总结:
### 第 1 轮 react_agent 自然语言总结
{summary}
...

请评估覆盖情况，决定是否追问或结束。
[当前不允许提问] react_agent 已开始执行，ask_user 必须为 false。
[记忆提示] 上面已附上你之前各轮的评估记录，请保持覆盖度判断的连续性...
```

### 2.5 跨轮记忆（`_build_user_agent_history`）

第 2 轮起从 `Conversation` 表加载 `role=user_agent, type=evaluation, round_idx<current` 的记录，提取 `reasoning` 字段（含 covered/missing/判断/追问）。

**超限裁剪策略**（`MAX_HISTORY_MSG_CHARS=3000`，`MAX_HISTORY_TOTAL_CHARS=12000`）：
- 优先级 2：`missing` 非空（还有未覆盖项，对决策更有参考价值）
- 优先级 1：`done=false`
- 优先级 0：其他
- 同优先级 FIFO 丢最早轮次

### 2.6 ask_user 流程（仅 round 0）

1. user_agent 输出 `ask_user=true + questions`
2. 后端校验 questions 结构，规范化 id/type/options，过滤掉 LLM 误加的"补充"问题
3. **后端追加固定 `SUPPLEMENT_QUESTION`**（id=`_supplement`，"是否有其他补充?"）作为最后一题
4. orchestrator 推送 `question` 事件 + 落库 `Conversation(role=user_agent, type=question)`
5. `set_pending_question` + 阻塞 `wait_for_answers`
6. 用户提交答案 → `_format_user_answers` 格式化为 `[用户澄清] Q1/A1/Q2/A2...` 拼回 `effective_intent`
7. 重新调 `run_user_agent`（`ask_round+1`）
8. 达到 `MAX_ASKS=2` 时强制 `ask_user=false`

### 2.7 后置约束

- 非 round 0 或已达 `MAX_ASKS` → 强制 `ask_user=false`
- `ask_user=true` 但 `questions` 空 → 关闭提问
- JSON 解析失败 → 兜底 `done=true, results=[]`，避免无意义重跑

### 2.8 落库（`_record_user_agent`）

- `content`：精简显示（追问内容 / "评估完成" / "请求用户澄清(N 个问题)"）
- `reasoning`：完整评估（已覆盖/未覆盖/判断/追问/done 标记），供刷新页面回看 + 跨轮记忆加载

---

## 3. 内置 react_agent 详解

**文件**：[backend/app/agents/react_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/react_agent.py)

### 3.1 核心特征

- **ReAct 循环**：思考 → 工具调用 → 观察 → 再思考，最多 `MAX_ITERATIONS=30` 次
- **流式 LLM 调用**：`_stream_llm_response` 累积 `reasoning_delta / content_delta / tool_call_deltas`，实时推送 `thinking_delta`
- **跨轮记忆**：三级压缩策略（Level 0 完整 → Level 1 丢工具摘要 → Level 2 LLM 压缩早期轮次）
- **plan 状态机**：代码维护权威 `current_plan`，工具调用前推断 step 标 `in_progress`，LLM 在 thinking 里输出 `<plan>` 时合并（信任 LLM 的 `done` 标注）
- **循环检测**：连续相同调用 + 滑动窗口低多样性检测，强制转入总结
- **Hermes 风格 tool_call 兜底**：从 content 文本解析 `<tool_call>{...}</tool_call>` 块（适配 GLM/Qwen 思考模式下工具调用写在正文的情况）

### 3.2 输入参数（`run_react_agent`）

```python
def run_react_agent(
    task, db,
    round_idx=1,
    followup_query: str | None,        # None=第一轮,用 task.user_input
    client: LLMClient | None,          # None 时回退 env 默认
    repo_context: str | None,          # 第 1 轮专用,主动 clone 后的仓库上下文
    previous_plan: list[dict] | None,  # 上一轮结束时的 plan(跨轮续接)
) -> (results: list, summary: str, final_plan: list[dict])
```

### 3.3 上下文构造

#### System Prompt（`REACT_AGENT_SYSTEM_PROMPT`）

固定模板 + 末尾追加两段记忆：

1. **分项目记忆**（`build_react_agent_memory_section`）
   - 来源：`Project.memory_summary`（优先）/ `memory_content`（回退截断）
   - 引导语："Prioritize checking Hard Constraints and Known Issues"
   - 末尾附："Full memory available via read_file /home/user/.agent_memory/project_memory.md"
   - 完整记忆已由 orchestrator 在 clone 后写入沙箱该路径（突破字数限制）

2. **全局长期记忆**（`build_global_memory_section`）
   - 来源：`UserMemory.content`
   - 注入执行侧而非仅 user_agent：执行侧在沙箱干活，"怎么做"的知识直接影响执行正确性

#### User Message

**第 1 轮（`followup_query=None`）**：
```
{task.user_input}
仓库地址: {repo_url}
分支: {branch}

[仓库已预先 clone,无需你再调用 clone_repo]
{repo_context}
请直接基于上述仓库路径开始审计(用 read_file / search_code / list_files 等)...
```

**追问轮（`followup_query` 非空）**：
```
基于之前的审计结果，现在请针对以下问题继续检查(不需要重新 clone 仓库):
仓库路径(已 clone,直接用这个路径调 read_file/search_code/list_files): {repo_path}

[之前轮次的对话记忆]
{history_prefix from _build_history_context}

[本轮 user_agent 追问]
{followup_query}
```

### 3.4 跨轮记忆（`_build_history_context`，三级压缩）

从 `Conversation` 表加载 `round_idx < current` 的所有记录（排除 `history_compress` 缓存），按轮分组，每轮构造：

- **full (Level 0)**：工具调用摘要（intent + 结果片段 200 字符）+ react_agent 总结 + user_agent 评估
- **compact (Level 1)**：丢工具摘要，只保留 react_agent 总结 + user_agent 评估
- **priority**：2=missing 非空，1=done=false，0=其他

**超限处理顺序**：
1. 全部 Level 0 ≤ `MAX_HISTORY_TOTAL_CHARS=12000` → 直接用
2. 超限 → 按优先级降级（低的先降，同优先级 FIFO），全 Level 1 还超 → 进入 Level 2
3. **Level 2**：保留最近 `HISTORY_KEEP_RECENT=1` 轮 Level 1，早期轮次调 LLM 压缩
   - 压缩 prompt：`_HISTORY_COMPRESS_PROMPT`，关闭 thinking 模式加速
   - **带缓存 + 增量压缩**：`_get_or_create_compressed` 查 `type=history_compress` 缓存记录，部分覆盖时增量压缩（旧摘要 + 新轮次），结果落库为新缓存
4. 无 client 或压缩失败 → 兜底强制截断（`_truncate_segments`）

**单条截断**：`MAX_HISTORY_MSG_CHARS=3000`，工具摘要 `MAX_TOOL_HISTORY_CHARS=2000`

### 3.5 ReAct 循环细节

每个迭代：
1. **暂停检查点**：`wait_if_paused(task.id)`（粗粒度，工具调用前还有细粒度检查点）
2. **用户补充消息注入**：`drain_user_messages(task.id)` 取用户在运行中/暂停中追加的消息，合并为一条 user 消息注入 `messages`
3. **流式调 LLM**：`_stream_llm_response` 返回 `reasoning_full / content_full / tool_calls_full / finish_reason`
4. **落库 thinking**：`type=thinking, publish_event=False`（流式卡片已展示，避免重复推 SSE）
5. **提取 plan**：`_extract_plan(content_full)` 从 `<plan>...</plan>` 块解析，`_merge_plan` 合并到 `current_plan`
6. **tool_calls 兜底**：结构化 `tool_calls_full` 为空时，从 content 文本解析 `<tool_call>` 块
7. **结束判断**：`not tool_calls_full and finish_reason != "length"` → 真正结束，`content_full` 作为 summary
8. **执行工具**：`execute_tool(fn_name, fn_args)`，结果以 `role=tool` 消息加回 `messages`
   - 工具调用签名记录到 `recent_calls`（循环检测）
   - plan 推进：`_infer_step_from_tool` 根据 tool_name 关键词匹配 step.text，标 `in_progress`
9. **plan 提醒注入**：`_format_plan_reminder(current_plan)` 作为 system 消息加到 `messages`（可替换，避免累积）
10. **循环检测**：连续 `MAX_SAME_CALLS=3` 次相同调用，或滑动窗口 `LOOP_WINDOW_SIZE=6` 内不同签名 ≤ `LOOP_MIN_DISTINCT=2` → 强制转入总结

### 3.6 plan 状态机（代码 + LLM 双向同步）

参考 LangGraph Plan-and-Execute：
- 代码维护权威 `current_plan`，不依赖 LLM 每轮重写
- **代码推进**：工具调用前根据 `_TOOL_STEP_KEYWORDS` 表推断当前 step，标 `in_progress`（粗粒度）
- **LLM 确认**：LLM 在 thinking 里输出新 `<plan>` 时，`_merge_plan` 按 `step.text` 匹配，信任 LLM 的 `done` 标注（它有 tool_result 上下文，判断更准）
- **跨轮续接**：`previous_plan` 传入本轮启动时即注入为 system 提醒，避免重新规划已完成项

`_TOOL_STEP_KEYWORDS` 映射示例：
```python
"clone_repo":      ["克隆", "clone", "仓库"],
"search_code":     ["注入", "密钥", "反序列化", "ssrf", "路径", "认证", "授权", "审计", "代码审计", "search"],
"run_command":     ["执行", "运行", "跑", "测试", "构建", "build", "test", "run", "shell"],
```

### 3.7 返回值

```python
return [], summary, current_plan
# results 始终为空:结构化结果由 user_agent 在 done=true 时通过 results 字段输出
# summary: 本轮自然语言总结(供 user_agent 评估)
# final_plan: 本轮结束时的 plan 状态(供 orchestrator 传给下一轮)
```

---

## 4. 外部 CLI agent 集成

### 4.1 执行器抽象层

**文件**：[backend/app/agents/executor_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/executor_agent.py)

```
ExecutorAgent (ABC)
   ├─ BuiltinReactAgent    → 委托 react_agent.run_react_agent
   └─ ExternalCLIAgent     → 按 registry 用 importlib 加载 wrapper 的 run_*_func
```

**`get_executor(task)` 工厂**：
- `task.executor == "builtin"` → `BuiltinReactAgent`
- `task.executor` 在 registry 中 → `ExternalCLIAgent(agent_type)`
- 未知值 → 回退 builtin + warning

**统一契约**：
```python
.run(task, db, round_idx, followup_query, client, repo_context, previous_plan)
    -> (results: list, summary: str, final_plan: list[dict])
```
- `client` 仅内置 provider 使用；外部 CLI 忽略（CLI 自带模型配置）

### 4.2 registry 注册表

**文件**：[backend/app/agents/registry.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/registry.py)

`AGENT_REGISTRY` 声明每种 CLI 的：
- `display_name / description`：前端展示
- `credential_fields`：凭证字段定义（前端表单渲染 + 后端校验）
  - `type=secret`：加密存储（API Key / PAT）
  - `type=text`：明文存储（base_url / model）
  - `type=select`：下拉选择（provider_type / wire_api）
- `sandbox`：沙箱内运行配置
  - `bin_config_key` / `bin_default`：CLI 可执行文件名
  - `install_cmd_*`：安装命令
  - `acp_args`：ACP 启动参数
  - `credential_env`：凭证 → 环境变量映射
  - `credential_env_defaults`：默认环境变量（如 `KIMI_MODEL_NAME=kimi-for-coding`）
  - `inject_cli_model_args`：是否支持 `--model` / `--reasoning-effort` CLI 参数
  - `bridge_script`：使用哪个 bridge（默认 `acp_bridge`，Codex 用 `codex_bridge`）
- `executor_module` / `executor_func`：wrapper 入口（延迟导入）

**已注册类型**：

| agent_type | CLI | 启动命令 | 模型注入方式 | 凭证注入方式 |
|-----------|-----|---------|-------------|-------------|
| `qoder_cli` | Qoder CLI (国际版) | `qodercli --acp --yolo` | `--model` CLI 参数 | `QODER_PERSONAL_ACCESS_TOKEN` env |
| `qoder_cli_cn` | Qoder CN CLI (国内版) | `qoderclicn --acp --yolo` | `--model` CLI 参数 | `QODERCN_PERSONAL_ACCESS_TOKEN` env |
| `kimi_cli` | Kimi Code CLI | `kimi acp` | `KIMI_MODEL_*` env 系列 | `KIMI_MODEL_API_KEY` env + `set_config_option(mode=yolo)` |
| `hermes_cli` | Hermes CLI | `hermes acp` | `~/.hermes/config.yaml` | 按 provider 动态映射（`OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` / ...）+ `HERMES_YOLO_MODE=1` |
| `codex_cli` | Codex CLI | `codex exec --json` (经 codex_bridge 翻译) | `~/.codex/config.toml` | `CODEX_API_KEY` env |

### 4.3 ACP 基础设施

**文件**：[backend/app/agents/acp_base.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/acp_base.py)

提供共享组件，被所有 wrapper 复用：

#### ACPClient（HTTP/SSE 客户端）

通过 HTTP 与沙箱内的 `acp_bridge.py` 通信，bridge 把 HTTP 请求转换为 CLI 的 stdio ACP（JSON-RPC over newline-delimited JSON），响应以 SSE 流式返回。

**核心方法**：
- `initialize()`：ACP 握手，交换协议版本（`ACP_PROTOCOL_VERSION=1`）和能力
- `authenticate(method_id)`：ACP 认证（实际流程中跳过，凭证经环境变量自动认证）
- `new_session(cwd)`：创建会话，返回 `session_id`
- `set_config_option(session_id, config_id, value)`：运行时切换配置（Kimi 用此设 `mode=yolo`）
- `prompt(session_id, prompt, on_event)`：发送 prompt，流式接收通知
- `cancel(session_id)`：取消正在进行的 prompt
- `health()`：健康检查

**`_rpc` 内部**：所有 POST `/rpc` 返回 SSE，通知（有 method 无 id）通过 `on_event` 回调处理，最终响应（id 匹配）返回其 `result`。

#### _ACPRecorder（原始响应落盘）

每个 task + round 一份 JSONL 文件：`{backend}/logs/acp/{task_id}_r{round_idx}_{YYYYmmdd_HHMMSS}.jsonl`

在 `_rpc` 的 SSE 循环里，每读到一行就 `record_raw`，**在任何解析/过滤之前落盘**，完整保留：
- 所有 `data:` 行（不论是否合法 JSON）
- 非 `data:` 行（SSE 注释 / event: 等）
- HTTP 错误响应体（非 200 时）
- 元信息（请求开始/结束标记）

每行 JSONL：`{"seq": N, "ts": "ISO", "kind": "line|http_error|meta", "raw": "..."}`

#### _ACPCollector（事件翻译 + 落库）

把 ACP `session/update` 通知翻译为 `event_bus` 事件 + 落库 `Conversation`：

| ACP sessionUpdate | event_bus 事件 | Conversation 落库 |
|-------------------|---------------|-------------------|
| `thought_chunk` / `thinking` / `reasoning` | `thinking_delta(phase=reasoning)` | (累积到 reasoning_buf) |
| `agent_message_chunk` | `thinking_delta(phase=content)` | (累积到 content_buf) |
| `tool_call` | `conversation(type=tool_call)` | 是 |
| `tool_call_update` (status=in_progress) | `conversation_update` (节流) | 否（临时态） |
| `tool_call_update` (status=completed) | `conversation(type=tool_result)` + `conversation_update` | 是 |
| `plan` | `plan` | 否 |
| `error` | `thinking_delta(phase=error)` | 否 |

**迭代切段**：ACP 一次 prompt 内部可能含多次 ReAct 迭代（thought → message → tool_call → tool_result → ...）。按 `tool_call` 切段：每遇到 `tool_call` 就结束当前迭代（推 `phase=end` + 落库一条 thinking），开启新迭代（新 `conv_id`），让前端以独立流式卡片展示。

**工具调用解析**：
- Qoder CN：`rawInput` 在 `tool_call` 事件一次性给出
- Kimi：参数经 `tool_call_update(in_progress)` 增量构建（每次是完整累积文本，非 delta），completed 时用累积的 `input_text` 补全 tool_call conversation
- intent 生成：`_build_tool_intent_detail` 按工具名（Agent / Bash / 其他）生成人类可读一句话，末尾带 `[tool_name]` 标签

#### 通用运行流程（`run_acp_agent`）

```
1. set_current_task(task_id_str, task.scenario)
2. 校验 agent_type 已注册 + SANDBOX_MODE != "mock"
3. _load_credentials(db, user_id, agent_type)
   → 从 UserAgentConfig 加载加密凭证,decrypt_secret 解密
4. credential_env_builder(credentials) 或 _build_credential_envs(credentials, agent_type)
   → 按 registry.credential_env 映射为环境变量 dict
5. sandbox_tools._get_or_create_session(task_id_str)
   → 复用 orchestrator 预 clone 的沙箱会话
6. _load_project_memory_summary(db, task) + _load_global_memory(db, task)
7. _ensure_cli_env(session, agent_type)
   → 创建 bridge 脚本目录 + 写入 bridge 脚本 + 检查 CLI(不可用则安装)
8. pre_bridge_hook(session, credentials, agent_type)  [wrapper 钩子]
   → Codex/Hermes 用此写 ~/.codex/config.toml / ~/.hermes/config.yaml
9. _start_acp_bridge(session, credential_envs, task, agent_type)
   → 后台启动:python3 acp_bridge.py --port 8088 --bin {cli_bin} --args '{json}'
   → 凭证经 envs 注入 bridge 进程,CLI 子进程继承
10. _wait_for_bridge_ready(session, execution_id, endpoint_url, ...)
    → 健康检查轮询(30s 超时,失败时读 bridge 日志辅助排查)
11. ACPClient(endpoint_url, endpoint_headers, recorder=_ACPRecorder(...))
12. client.initialize()
    → 跳过 authenticate(凭证经环境变量自动认证)
13. client.new_session(cwd=repo_path or "/home/user")
14. post_session_setup(client, session_id, task)  [wrapper 钩子]
    → Kimi 用此调 set_config_option(mode=yolo, thinking=effort)
15. _build_prompt_message(task, round_idx, followup_query, repo_context, repo_path,
                          previous_plan, memory_summary, global_memory)
    → 构造发给 CLI 的 prompt(与内置 react_agent 对齐)
16. _add_conversation(role=user, type=question, content=user_msg)
17. collector = _ACPCollector(task, db, round_idx)
18. client.prompt(session_id, [{"type":"text","text":user_msg}], on_event=collector)
19. recorder.close() + collector.close()
20. client.close() + _stop_acp_bridge(session, bridge_exec_id)
21. summary = collector.content_full or "第 N 轮完成(...)"
22. current_plan = _extract_plan(collector.content_full) or previous_plan
23. return [], summary, current_plan
```

### 4.4 Prompt 消息构造（`_build_prompt_message`）

**第 1 轮**：
```
{task.user_input}
仓库地址: {repo_url}
分支: {branch}

[仓库已预先 clone,无需你再调用 clone_repo]
{repo_context}
请直接基于上述仓库路径开始审计。
```

**追问轮**：
```
基于之前的审计结果，现在请针对以下问题继续检查(不需要重新 clone 仓库):
仓库路径(已 clone): {repo_path}

[本轮追问]
{followup_query}
```

**每轮末尾追加**（与内置 react_agent system prompt 行为一致）：
- 若有 `previous_plan`：`_format_plan_reminder(previous_plan)`（注入 plan 状态让 CLI 续接进度）
- 若有 `memory_summary`：`[项目记忆摘要] ... 完整项目记忆可 read_file /home/user/.agent_memory/project_memory.md 查阅`
- 若有 `global_memory`：跨项目通用经验段

> **注意**：CLI agent 不像内置 react_agent 那样维护 `messages` 列表，每次 prompt 都是独立的 user 消息。跨轮记忆主要依赖：
> 1. `previous_plan` 注入（plan 状态续接）
> 2. 项目记忆 + 全局记忆（每轮注入）
> 3. CLI 自身的会话恢复机制（如 Codex 的 `codex exec resume <thread_id>`）

### 4.5 wrapper 层差异

各 wrapper 是薄封装，仅实现 CLI 特有逻辑，通过回调注入 `run_acp_agent`：

| wrapper | 文件 | 特有逻辑 |
|---------|------|---------|
| `qoder_cli_agent` | [qoder_cli_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/qoder_cli_agent.py) | 无（`--yolo` 在 acp_args 中，模型经 `--model` CLI 参数）；测试时强制 `Qwen3.6-Flash + low` 最小化 credits |
| `kimi_cli_agent` | [kimi_cli_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/kimi_cli_agent.py) | `_kimi_post_session_setup`：session/new 后调 `set_config_option(mode=yolo, thinking=effort)` |
| `hermes_cli_agent` | [hermes_cli_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/hermes_cli_agent.py) | `_hermes_credential_env_builder`：按 provider 动态映射 API Key 环境变量名；`_hermes_pre_bridge_hook`：写 `~/.hermes/config.yaml`（模型/provider/base_url），注入 `HERMES_YOLO_MODE=1` |
| `codex_cli_agent` | [codex_cli_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/codex_cli_agent.py) | `_codex_pre_bridge_hook`：写 `~/.codex/config.toml`（模型/provider/approval_policy=never/sandbox_mode=danger-full-access）；使用 `codex_bridge.py`（非默认 `acp_bridge`） |

### 4.6 bridge 脚本

两个 bridge 脚本由 `_BRIDGE_SOURCES` 索引，按 `registry.sandbox.bridge_script` 选择：

- **`acp_bridge.py`**（默认）：通用 ACP stdio 桥接，适用于原生支持 ACP 的 CLI（Qoder / Kimi / Hermes）
- **`codex_bridge.py`**：Codex 专用，把 `codex exec --json` 的 JSONL 事件翻译为 ACP 通知（Codex 不原生支持 ACP）

bridge 监听 `ACP_BRIDGE_PORT=8088`，凭证经环境变量注入，CLI 子进程继承。

---

## 5. 上下文传递机制总结

### 5.1 上下文传递维度

```
┌─────────────────────────────────────────────────────────────┐
│                     orchestrator                            │
│  effective_intent(用户澄清后) / task.checklist /           │
│  react_summaries / current_plan(跨轮) / git_tokens /       │
│  allowed_skills / repo_path / repo_context                  │
└──────┬──────────────────────────────────────┬──────────────┘
       │                                      │
       ▼                                      ▼
┌──────────────┐                     ┌──────────────────┐
│  user_agent  │                     │  ExecutorAgent   │
│              │                     │                  │
│  输入:        │                     │  输入:            │
│  - user_intent                      │  - task.user_input
│  - react_summaries (跨轮)          │  - followup_query │
│  - task_checklist                   │  - repo_context (仅 round 1)
│  - repo_context (仅 round 0)       │  - previous_plan (跨轮) │
│  - history (自己之前各轮评估)      │  - client (仅 builtin) │
│  - User Profile + 全局记忆 + 项目记忆精简版           │
│              │                     │  - 分项目记忆 + 全局记忆 │
│  输出:        │                     │                  │
│  - covered/missing                  │  输出:            │
│  - followup_query                   │  - summary        │
│  - done + results + grouping        │  - final_plan     │
│  - ask_user + questions (round 0)   │                  │
└──────────────┘                     └──────────────────┘
```

### 5.2 跨轮记忆传递路径

| 传递路径 | 机制 | 字符上限 |
|---------|------|---------|
| orchestrator → react_agent | `previous_plan` 参数 | - |
| orchestrator → user_agent | `react_summaries` 列表 | - |
| user_agent 跨轮自记忆 | `_build_user_agent_history` 从 Conversation 表加载 | 单条 3000，总 12000 |
| react_agent 跨轮自记忆 | `_build_history_context` 三级压缩 | 总 12000（Level 2 LLM 压缩） |
| react_agent → user_agent | react_agent 落库 `type=thinking` 的 content（即 summary），user_agent 通过 `react_summaries` 接收 | - |
| user_agent → react_agent | user_agent 落库 `type=evaluation` 的 reasoning，react_agent 通过 `_build_history_context` 加载 | - |
| 长期记忆 → user_agent | `build_user_agent_memory_section` 注入 system prompt | 各段 2000 |
| 长期记忆 → react_agent | `build_react_agent_memory_section` + `build_global_memory_section` 注入 system prompt | 各段 2000 |
| 长期记忆 → CLI agent | `_load_project_memory_summary` + `_load_global_memory` 注入 prompt 末尾 | 各段 2000 |
| 完整项目记忆 → 沙箱 | orchestrator clone 后 `write_project_memory_file` 写入 `/home/user/.agent_memory/project_memory.md` | 无限制（react_agent / CLI 可 read_file 查阅） |

### 5.3 用户交互上下文

| 交互类型 | 触发条件 | 传递方式 |
|---------|---------|---------|
| **澄清提问**（round 0） | user_agent 输出 `ask_user=true` | orchestrator 推 `question` 事件 + 阻塞 `wait_for_answers` → 答案格式化拼回 `effective_intent` |
| **checklist 确认**（round 0） | user_agent 输出 `checklist` | orchestrator 推 `checklist_review` 事件 + 阻塞 `wait_for_checklist_confirmation` → 确认后落库 `task.checklist` |
| **运行中追加消息** | 用户在对话界面输入框发消息 | API 端点落库 `Conversation(role=user, type=message)` + 推 SSE；react_agent 每个迭代开头 `drain_user_messages` 注入 `messages` |
| **完成后重启** | 任务 COMPLETED 后用户追加消息 | `resume_audit_with_message`：用户消息拼到 `task.user_input` 后面作为 `effective_intent`，从 Conversation 表加载 `react_summaries`，重启协作循环 |

### 5.4 事件流（event_bus）

orchestrator / user_agent / react_agent / CLI agent 都通过 `event_bus.publish(task_id, event_type, payload)` 推送事件，前端通过 SSE 实时接收：

| 事件类型 | 触发者 | 用途 |
|---------|--------|------|
| `status` | orchestrator | 任务状态变更（status + current_stage） |
| `conversation` | orchestrator / react_agent / CLI agent | 新对话记录（thinking / tool_call / tool_result / evaluation / question / summary / error） |
| `conversation_update` | CLI agent (Kimi) | 更新已有 conversation 的 content（节流推送） |
| `thinking_delta` | user_agent / react_agent / CLI agent | 流式思考增量（phase: start / reasoning / content / error / end） |
| `plan` | react_agent / CLI agent | plan 状态更新（round_idx + steps） |
| `question` | orchestrator | 用户澄清提问（ask_round + questions + reasoning） |
| `checklist_review` | orchestrator | checklist 确认请求（checklist + reasoning） |
| `done` / `error` | orchestrator | 任务终止事件 |

---

## 6. 关键设计点

### 6.1 职责分离

- **user_agent 不调工具**：只评估和追问，避免与 react_agent 职责重叠
- **react_agent 不管理 task 状态**：只跑一轮返回结果，由 orchestrator 控制 task 状态
- **结构化结果由 user_agent 整理**：react_agent 只输出自然语言 summary，`results + grouping` 由 user_agent 在 `done=true` 时输出
- **执行器抽象**：orchestrator 通过 `get_executor(task)` 拿 provider，无需关心底层是内置 LLM 循环还是外部 CLI 协议

### 6.2 防止 LLM 反复摇摆

- **user_agent 跨轮自记忆**：第 2 轮起注入之前各轮评估，提示"之前已标 covered 的类别，本轮若 react_agent 未推翻结论，继续保持"
- **优先级裁剪**：missing 非空的轮次优先保留（对决策更有参考价值）
- **react_agent 三级压缩**：跨轮记忆超限时按优先级降级，最终 LLM 压缩早期轮次

### 6.3 防止死循环

- **react_agent 循环检测**：连续相同调用 + 滑动窗口低多样性检测，强制转入总结
- **MAX_ROUNDS=4**：协作轮次上限
- **MAX_ITERATIONS=30**：单轮 ReAct 迭代上限
- **MAX_ASKS=2**：用户澄清提问上限

### 6.4 暂停检查点

- **粗粒度**：每轮开始前 + react_agent 跑完后、user_agent 评估前
- **细粒度**：react_agent 每个迭代边界 + 工具调用前

### 6.5 资源清理（finally 块）

`run_dual_agent_audit` 和 `resume_audit_with_message` 的 finally 块清理：
- `clear_pending_question` / `clear_pending_checklist` / `clear_pause_state` / `clear_user_messages`
- `sandbox_tools.mark_task_completed`（延迟关闭沙箱，TTL 1 小时惰性清理）
- 推送 `done` / `error` 终止事件
- `finish_task`（通知事件总线任务结束）

---

## 7. 文件索引

| 文件 | 职责 |
|------|------|
| [orchestrator.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/orchestrator.py) | 双智能体协作编排（round 0 评估 + 协作循环 + resume） |
| [user_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/user_agent.py) | user_agent 实现（评估 / checklist 生成 / ask_user / 跨轮自记忆） |
| [react_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/react_agent.py) | 内置 ReAct 智能体（流式 LLM / 工具调用 / plan 状态机 / 三级压缩跨轮记忆 / 循环检测） |
| [executor_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/executor_agent.py) | 执行器抽象层（BuiltinReactAgent + ExternalCLIAgent + 工厂） |
| [registry.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/registry.py) | Agent 类型注册表（凭证字段 + 沙箱配置 + executor 入口） |
| [acp_base.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/acp_base.py) | ACP 基础设施（ACPClient + _ACPCollector + _ACPRecorder + bridge 管理 + 通用 run_acp_agent） |
| [acp_bridge.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/acp_bridge.py) | 通用 ACP stdio 桥接脚本（写入沙箱运行） |
| [codex_bridge.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/codex_bridge.py) | Codex 专用 bridge（codex exec --json JSONL → ACP 翻译） |
| [qoder_cli_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/qoder_cli_agent.py) | Qoder CLI wrapper（薄封装） |
| [kimi_cli_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/kimi_cli_agent.py) | Kimi CLI wrapper（post_session_setup 设 yolo 模式） |
| [hermes_cli_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/hermes_cli_agent.py) | Hermes CLI wrapper（动态凭证映射 + config.yaml） |
| [codex_cli_agent.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/agents/codex_cli_agent.py) | Codex CLI wrapper（pre_bridge_hook 写 config.toml） |
| [memory_injection.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/services/memory_injection.py) | 记忆注入服务（User Profile / 全局记忆 / 项目记忆） |
| [user_interaction.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/user_interaction.py) | 用户交互状态管理（pending question / checklist 阻塞等待） |
| [user_messages.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/user_messages.py) | 用户补充消息队列（运行中/暂停中追加） |
| [pause_controller.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/pause_controller.py) | 暂停/恢复控制器 |
| [event_bus.py](file:///c:/Users/njwjx/Desktop/coding/AgentPair/backend/app/event_bus.py) | 事件总线（publish / SSE 订阅） |
