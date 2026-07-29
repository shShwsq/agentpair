"""安全审计场景

首个场景实现。把安全专用的 checklist、prompt、工具白名单集中到本模块,
其他场景可参照实现 Scenario 协议。
"""
from typing import Any

from app.scenarios.base import Scenario, register_scenario


class SecurityAuditScenario:
    """代码安全审计场景"""

    id = "code_security_audit"
    name = "代码安全审计"

    # ---------- checklist(给 user_agent 用) ----------

    @property
    def checklist(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "injection",
                "name": "注入类",
                "cwe": "CWE-78, CWE-89, CWE-94",
                "description": "SQL 注入、命令注入、模板注入、LDAP 注入等",
                "checklist": [
                    "SQL 拼接:cursor.execute / ORM 的 raw / extra(where=)",
                    "命令注入:os.system / subprocess + shell=True / os.popen",
                    "模板注入:render_template_string / Jinja2 from_string",
                    "代码注入:eval / exec / pickle.loads / yaml.load(unsafe)",
                    "表达式注入:__import__ / getattr(用户输入)",
                ],
            },
            {
                "id": "auth",
                "name": "认证与授权",
                "cwe": "CWE-287, CWE-306, CWE-862, CWE-863, CWE-798",
                "description": "认证逻辑、授权校验、会话管理、硬编码凭证",
                "checklist": [
                    "硬编码密码/密钥:password = 'xxx' / api_key = 'xxx' 字面量",
                    "弱密码哈希:MD5/SHA1 用于密码 / 无 salt",
                    "JWT 验证缺失:verify=False / algorithm=none / 不验签",
                    "授权缺失(IDOR):GET/PUT/DELETE 只检查登录不检查所属",
                    "会话管理:session 永不过期 / 弱 session key",
                ],
            },
            {
                "id": "deserialization",
                "name": "反序列化",
                "cwe": "CWE-502",
                "description": "不安全的反序列化,可能导致 RCE",
                "checklist": [
                    "pickle.loads / pickle.load(用户输入)",
                    "yaml.load(未指定 SafeLoader)",
                    "marshal.loads",
                    "json.loads 配合 eval/json-to-object(自定义 decoder)",
                    "shelve / configparser 滥用",
                ],
            },
            {
                "id": "ssrf",
                "name": "SSRF / 外部请求",
                "cwe": "CWE-918",
                "description": "服务端请求伪造,访问内网或敏感端点",
                "checklist": [
                    "requests.get(用户可控 URL)",
                    "urllib.request.urlopen(用户输入)",
                    "httpx / aiohttp 接收用户 URL 不校验",
                    "文件协议:file:// / gopher:// 未屏蔽",
                    "重定向跟踪导致 SSRF",
                ],
            },
            {
                "id": "path_traversal",
                "name": "路径穿越 / 文件操作",
                "cwe": "CWE-22, CWE-73",
                "description": "文件路径拼接用户输入,导致越权访问",
                "checklist": [
                    "open(用户输入路径)",
                    "os.path.join(用户输入)未做规范化",
                    "send_file / send_from_directory(用户输入)",
                    "Path(用户输入).resolve() 未限制根目录",
                    "zip 解压(zip slip)",
                ],
            },
            {
                "id": "crypto",
                "name": "加密与随机数",
                "cwe": "CWE-327, CWE-328, CWE-330",
                "description": "弱加密算法、弱随机数、硬编码 IV/Salt",
                "checklist": [
                    "弱算法:MD5/SHA1 用于密码或签名",
                    "ECB 模式:Mode.ECB / 默认 mode",
                    "硬编码 IV/Salt:bytes 字面量",
                    "弱随机数:random 用于 token/secret(应 secrets)",
                    "TLS 验证缺失:verify=False / ssl._create_unverified_context",
                ],
            },
            {
                "id": "deps",
                "name": "依赖漏洞",
                "cwe": "CWE-1035, CWE-1104",
                "description": "依赖库的已知 CVE,需用 query_cve 查询",
                "checklist": [
                    "Python: requirements.txt / pyproject.toml",
                    "Node: package.json / package-lock.json",
                    "Go: go.mod / go.sum",
                    "Java: pom.xml / build.gradle",
                    "对每个依赖调 query_cve 查已知漏洞",
                ],
            },
        ]

    # ---------- user_agent prompt ----------

    @property
    def user_agent_prompt(self) -> str:
        return """你是一个"用户代理"智能体,你的角色是扮演一个挑剔的安全工程师,
正在审视 react_agent(代码审计智能体)的审计结果,决定是否需要它继续追问。

## 你的职责
1. 对照 checklist(7 大安全类别),评估 react_agent 的审计是否覆盖完整
2. 针对未覆盖或覆盖不足的类别,构造具体的追问请求,让 react_agent 再跑一轮
3. 当 checklist 全部覆盖且结论明确时,宣布审计完成

## 关键原则
- 你**不直接审计代码**,只评估 react_agent 的结果
- 你要像一个资深安全工程师 review 初级工程师的工作
- 你要"挑剔":宁可多问一轮,不要漏掉一个类别
- 追问要具体:不要说"再查查认证",要说"检查 JWT 验证是否缺失 verify=False,以及 IDOR(未检查资源归属)"
- 若 react_agent 已经覆盖某类别并给出明确结论(有/无),不要无意义追问
- 若 react_agent 的结论模糊("可能有问题"但没有具体文件/行号),算作未覆盖

## 输出格式(严格 JSON,不要任何 markdown 代码块)
{
  "covered": ["injection", "deps"],
  "missing": ["auth", "ssrf"],
  "reasoning": "injection 类已查到 SQL 注入,deps 已查 CVE。auth 未提及,ssrf 未提及。",
  "followup_query": "请检查认证与授权模块:1) JWT 验证是否缺失 verify=False;2) 是否存在 IDOR(DELETE/PUT 只检查登录不检查所属)。同时检查 SSRF:requests.get 是否接收用户可控 URL。",
  "done": false
}

字段说明:
- covered: 已覆盖的类别 id 列表(checklist 里的 id)
- missing: 未覆盖或覆盖不足的类别 id 列表
- reasoning: 你的判断依据(简短)
- followup_query: 给 react_agent 的追问指令(若 done=true,此字段可省略)
- done: 是否审计完成(missing 为空且所有类别结论明确时为 true)

## checklist(7 大类别)
{checklist_text}

## 何时返回 done=true
- covered 包含全部 7 个类别
- 且每个类别 react_agent 都给出明确结论(有漏洞/无漏洞/无法确定)
- 不要追求"绝对完美",7 个类别都覆盖了就结束

## 何时返回 done=false
- missing 非空,或某类别结论模糊
- followup_query 要具体到检查点,不要笼统说"再查查"
"""

    # ---------- react_agent prompt ----------

    @property
    def react_agent_prompt(self) -> str:
        return """你是一个专业的代码安全审计智能体,使用 ReAct 模式工作:思考 → 调用工具 → 观察结果 → 继续思考。

## 任务
审计用户指定的 GitHub 仓库,查找安全漏洞。

## 工作流程
1. 先调用 clone_repo 克隆仓库
2. **查看结构**:调用 list_files 查看根目录的文件和子目录(单层)。看到子目录后可再调 list_files 进入查看。**不要凭空猜测文件名**(README/Makefile/Dockerfile 等无扩展名文件常见,猜不准)
3. **依赖审计**:用 list_files 找到依赖清单(requirements.txt / package.json / go.mod / pom.xml 等),read_file 读取,对每个依赖调 query_cve 查已知 CVE
4. **SKILL 驱动审计(阶段 5 新增)**:调用 list_skills 查看可用技能,按需调 skill(skill_name=...) 获取详细指令。每个 skill 是预封装的多步审计操作,拿到 instructions 后按其指引调用 search_code / read_file 等底层工具执行。优先用 skill 而不是从零写搜索模式
5. **代码审计**:对 skill 未覆盖的类别,用 search_code 搜索各类危险模式(注入、硬编码密钥、反序列化、SSRF 等)
6. 对搜到的可疑点用 read_file 查看上下文,判断是否真的是漏洞
7. **SAST 补充**:若沙箱可用,调 run_semgrep 跑自动化静态分析(mock 模式会返回提示,跳过即可)
8. 汇总所有确认的漏洞,调用 submit_results 提交

## 计划清单(复杂任务时输出)
当任务涉及多个审计类别(如完整仓库审计)时,在**第一次正式回答(content)开头**先用 `<plan>` 标签列出计划,让用户能看到你接下来要做的步骤。格式:

<plan>
1. [pending] 克隆仓库并查看目录结构
2. [pending] 审计依赖漏洞(requirements.txt + query_cve)
3. [pending] 审计注入类(SQL/命令/模板/代码注入)
4. [pending] 审计认证与授权(硬编码密钥/JWT/IDOR)
5. [pending] 审计反序列化、SSRF、路径穿越、加密
6. [pending] 汇总结果并提交
</plan>

- 步骤数 3-8 项为宜,太细碎反而难看
- 状态标记:[pending] / [in_progress] / [done],首次输出全用 [pending]
- **后续每次思考时,在 content 开头重新输出更新后的 <plan>**(把已完成的标 [done]、正在做的标 [in_progress]),让用户实时看到进度
- 简单任务(如只查单个文件)可省略 plan,直接开干
- plan 只是给用户看进度,**不改变 ReAct 执行方式**,每步内部仍正常思考→调工具→观察

## 审计要点(参考 OWASP Top 10 + CWE Top 25)
- 注入类:SQL 拼接、命令注入、模板注入(eval/exec/cursor.execute/os.system)
- 认证与授权:硬编码密码、弱密码哈希、JWT 验证缺失
- 反序列化:pickle/yaml.load/marshal/eval
- SSRF:requests.get/urllib.request.urlopen 中用户可控 URL
- 硬编码密钥:API key、token、password 字面量
- 路径穿越:文件操作拼接用户输入
- 不安全加密:弱算法(MD5/SHA1 用于密码)、ECB 模式、硬编码 IV
- **已知漏洞**:依赖库的 CVE(通过 query_cve 查询,而非自己判断)

## 工具使用要点
- **list_files**:clone 后第一步必须调用,查看根目录结构。看到子目录后再调 list_files 进入。禁止凭空猜文件名
- **list_skills / skill(阶段 5)**:clone 后第二步调 list_skills 查可用技能。按场景类别(SSRF/SQL注入/硬编码密钥)调对应 skill 获取指令,然后按指令执行。**优先用 skill 而非手写搜索**
- **query_cve**:对每个依赖调一次,不要批量查
- **run_semgrep**:若返回 note 提示 mock 模式不可用,直接跳过
- **search_code**:对 skill 未覆盖的类别,自己写正则搜高危模式

## 输出规范
所有发现必须通过 submit_results 工具提交,每个 result 包含:
- title: 简短标题,如 "[high] CWE-89 SQL注入 src/main.py:42"
- content: 漏洞描述 + 修复建议
- metadata: 必须包含以下字段:
    - cwe: CWE 编号,如 "CWE-89"
    - severity: info / low / medium / high / critical
    - file_path: 文件路径
    - line_range: 行号或范围,如 "42" 或 "42-45"
    - remediation: 修复建议

CVE 类发现的 cwe 用 "CWE-1035",content 写明 CVE id 和受影响版本。

## 注意
- 不要漏报,但也不要误报。看上下文判断是否真的可利用
- 测试代码里(tests/、*_test.py)的发现标为 info
- **禁止重复 read 同一个文件**
- 单次审计控制在 20 轮以内
- 若无明显漏洞,也必须 submit_results(传空数组),并在最后一轮思考里说明已查范围
"""

    # ---------- 工具白名单 ----------

    @property
    def enabled_tools(self) -> list[str]:
        return [
            "clone_repo", "list_files", "read_file", "search_code",
            "query_cve", "run_semgrep",
            # 阶段 5:SKILL 机制
            "list_skills", "skill",
        ]

    # ---------- submit_results 工具定义 ----------
    # 通用结构:title + content + metadata,场景无关
    # 安全场景的 metadata 字段要求在 react_agent_prompt 里说明

    @property
    def submit_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "submit_results",
                "description": (
                    "提交本轮审计的所有结果。审计完成或确认无更多发现时必须调用。"
                    "若没有发现,传空数组 results=[]"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "description": "结果列表",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {
                                        "type": "string",
                                        "description": "简短标题",
                                    },
                                    "content": {
                                        "type": "string",
                                        "description": "详细内容/描述",
                                    },
                                    "metadata": {
                                        "type": "object",
                                        "description": "场景专用信息(如 cwe/severity/file_path 等)",
                                    },
                                },
                                "required": ["title", "content"],
                            },
                        },
                        "summary": {
                            "type": "string",
                            "description": "本轮审计的总结说明(已查范围、结论)",
                        },
                    },
                    "required": ["results"],
                },
            },
        }

    # ---------- result 格式化 ----------

    def format_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把 LLM 提交的 raw result 转成数据库存储格式

        安全场景:LLM 应在 metadata 里放 cwe/severity/file_path/line_range/remediation
        这里做兜底:若 metadata 缺字段,补默认值
        """
        metadata = raw.get("metadata") or {}
        # 兜底:确保关键字段存在
        metadata.setdefault("cwe", "CWE-Unknown")
        metadata.setdefault("severity", "info")
        metadata.setdefault("file_path", None)
        metadata.setdefault("line_range", None)
        metadata.setdefault("remediation", "")

        return {
            "title": raw.get("title", "(无标题)"),
            "content": raw.get("content", ""),
            "metadata": metadata,
        }

    # ---------- 前端声明(场景无关 UI 驱动,阶段 7) ----------

    @property
    def form_fields(self) -> list[dict[str, Any]]:
        """提交任务表单字段定义

        安全场景:repo_url(必填)+ branch(可选)+ note(可选)
        这些字段名与 params 对齐,前端提交时作为 params 的 key
        """
        return [
            {
                "name": "repo_url",
                "type": "url",
                "label": "GitHub 仓库地址",
                "required": True,
                "placeholder": "https://github.com/owner/repo",
                "description": "要审计的仓库地址",
            },
            {
                "name": "branch",
                "type": "text",
                "label": "分支",
                "required": False,
                "placeholder": "默认主分支",
            },
            {
                "name": "note",
                "type": "textarea",
                "label": "补充说明",
                "required": False,
                "placeholder": "如:重点关注认证模块、只审计 src/ 目录等",
            },
        ]

    @property
    def result_grouping(self) -> dict[str, Any]:
        """结果分组维度:按 severity 分组,固定枚举+顺序

        color 与前端 sev-* CSS class 对齐
        """
        return {
            "field": "severity",
            "type": "ordered",
            "values": [
                {"value": "critical", "label": "严重", "color": "critical", "order": 0},
                {"value": "high", "label": "高危", "color": "high", "order": 1},
                {"value": "medium", "label": "中危", "color": "medium", "order": 2},
                {"value": "low", "label": "低危", "color": "low", "order": 3},
                {"value": "info", "label": "提示", "color": "info", "order": 4},
            ],
            "default_label": "未分级",
            "default_color": "unknown",
        }

    @property
    def result_meta_fields(self) -> list[dict[str, Any]]:
        """结果 meta 字段展示:cwe + file_path(可点击跳转)+ line_range"""
        return [
            {"name": "cwe", "label": "CWE", "type": "text"},
            {"name": "file_path", "label": "文件", "type": "file"},
            {"name": "line_range", "label": "行号", "type": "text"},
        ]

    @property
    def coverage(self) -> dict[str, Any]:
        """覆盖度看板:维度派生自 checklist 的 7 大类别"""
        return {
            "dimensions": [
                {"id": c["id"], "name": c["name"], "description": c.get("description", "")}
                for c in self.checklist
            ],
        }


# 注册场景
_security_audit = SecurityAuditScenario()
register_scenario(_security_audit)
