"""通用代码审查场景

非安全场景示例,用于验证前端场景无关 UI 的泛化能力。
对照安全场景:不查漏洞,而是从可读性/可维护性/正确性/测试覆盖等维度做代码审查。

与安全场景的差异(验证泛化):
- form_fields:相同(repo_url/branch/note),但场景语义不同
- result_grouping:按 issue_type(可读性/正确性/性能/风格)分组,而非 severity
- result_meta_fields:无 cwe,改为 file_path + line_range + rule(规则名)
- coverage:维度派生自本场景 checklist(可读性/正确性/性能/测试/架构)
"""
from typing import Any

from app.scenarios.base import Scenario, register_scenario


class CodeReviewScenario:
    """通用代码审查场景

    审查维度(非安全):可读性、正确性、性能、测试覆盖、架构合理性
    """

    id = "code_review"
    name = "代码审查"

    # ---------- checklist(给 user_agent 用) ----------

    @property
    def checklist(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "readability",
                "name": "可读性",
                "description": "命名、注释、复杂度、代码结构是否易于理解",
                "checklist": [
                    "命名:变量/函数/类命名是否达意,无 a/b/tmp 等无意义名",
                    "函数长度:单函数超过 50 行或圈复杂度过高",
                    "注释:复杂逻辑缺注释,或注释与代码不符",
                    "重复代码:多处复制粘贴的逻辑未抽取",
                ],
            },
            {
                "id": "correctness",
                "name": "正确性",
                "description": "逻辑错误、边界条件、异常处理",
                "checklist": [
                    "边界条件:空列表/零除/越界未处理",
                    "异常处理:裸 except / 吞异常 / 异常类型过宽",
                    "资源泄漏:open/连接未 close,无 with 语句",
                    "并发问题:共享变量无锁、竞态条件",
                ],
            },
            {
                "id": "performance",
                "name": "性能",
                "description": "明显的性能问题(算法复杂度、N+1、不必要的 IO)",
                "checklist": [
                    "N+1 查询:循环内查数据库",
                    "O(n²) 嵌套循环处理大数据",
                    "不必要的 IO:循环内读文件/发请求",
                    "内存:大列表一次性加载未分页/流式处理",
                ],
            },
            {
                "id": "testing",
                "name": "测试覆盖",
                "description": "测试是否存在、是否覆盖关键路径",
                "checklist": [
                    "测试存在性:有无 tests/ 目录或 test_*.py 文件",
                    "关键路径覆盖:核心业务逻辑是否有测试",
                    "边界测试:空值/异常输入是否有测试",
                    "测试质量:是否只测 happy path",
                ],
            },
            {
                "id": "architecture",
                "name": "架构合理性",
                "description": "分层、耦合、职责划分",
                "checklist": [
                    "分层:业务逻辑是否散落在路由/视图层",
                    "耦合:模块间直接依赖实现而非接口",
                    "职责:单个类/模块承担过多职责",
                    "配置:硬编码配置应抽到配置文件/env",
                ],
            },
        ]

    # ---------- user_agent prompt ----------

    @property
    def user_agent_prompt(self) -> str:
        return """你是一个"用户代理"智能体,你的角色是扮演一个挑剔的 Tech Lead,
正在审视 react_agent(代码审查智能体)的审查结果,决定是否需要它继续追问。

## 你的职责
1. 对照 checklist(5 大审查维度),评估 react_agent 的审查是否覆盖完整
2. 针对未覆盖或覆盖不足的维度,构造具体的追问请求,让 react_agent 再跑一轮
3. 当 checklist 全部覆盖且结论明确时,宣布审查完成

## 关键原则
- 你**不直接审查代码**,只评估 react_agent 的结果
- 你要像一个资深 Tech Lead review 初级工程师的 code review 报告
- 你要"挑剔":宁可多问一轮,不要漏掉一个维度
- 追问要具体:不要说"再查查性能",要说"检查 views.py 里是否有 N+1 查询(循环内 ORM 查询)"
- 若 react_agent 已经覆盖某维度并给出明确结论,不要无意义追问
- 若 react_agent 的结论模糊(只有"代码还行"没有具体文件/问题),算作未覆盖

## 输出格式(严格 JSON,不要任何 markdown 代码块)
{
  "covered": ["readability", "correctness"],
  "missing": ["performance", "testing", "architecture"],
  "reasoning": "readability 已指出命名问题,correctness 已发现异常吞没。performance/testing/architecture 未提及。",
  "followup_query": "请检查性能:1) views.py 是否有 N+1 查询(循环内 .filter()/.get());2) 是否有 O(n²) 嵌套循环。同时检查测试:是否存在 tests/ 目录,核心业务逻辑是否覆盖。",
  "done": false
}

字段说明:
- covered: 已覆盖的维度 id 列表(checklist 里的 id)
- missing: 未覆盖或覆盖不足的维度 id 列表
- reasoning: 你的判断依据(简短)
- followup_query: 给 react_agent 的追问指令(若 done=true,此字段可省略)
- done: 是否审查完成(missing 为空且所有维度结论明确时为 true)

## checklist(5 大维度)
{checklist_text}

## 何时返回 done=true
- covered 包含全部 5 个维度
- 且每个维度 react_agent 都给出明确结论(有问题/无问题/无法确定)
- 不要追求"绝对完美",5 个维度都覆盖了就结束

## 何时返回 done=false
- missing 非空,或某维度结论模糊
- followup_query 要具体到检查点,不要笼统说"再查查"
"""

    # ---------- react_agent prompt ----------

    @property
    def react_agent_prompt(self) -> str:
        return """你是一个专业的代码审查智能体,使用 ReAct 模式工作:思考 → 调用工具 → 观察结果 → 继续思考。

## 任务
审查用户指定的 GitHub 仓库的代码质量(非安全审查,关注可读性/正确性/性能/测试/架构)。

## 工作流程
1. 先调用 clone_repo 克隆仓库
2. **查看结构**:调用 list_files 查看根目录的文件和子目录(单层)。看到子目录后可再调 list_files 进入查看。不要凭空猜测文件名
3. **定位核心代码**:用 list_files 找到源码目录(src/ app/ 等),read_file 阅读关键模块
4. **逐维度审查**:
   - 可读性:命名、函数长度、重复代码、注释
   - 正确性:边界条件、异常处理、资源泄漏、并发
   - 性能:N+1 查询、嵌套循环、循环内 IO
   - 测试:测试文件存在性、关键路径覆盖
   - 架构:分层、耦合、职责划分
5. 用 search_code 搜索常见坏味道(裸 except / 循环内 query / 超长函数等)
6. 汇总所有确认的问题,调用 submit_results 提交

## 工具使用要点
- **list_files**:clone 后第一步必须调用,查看根目录结构
- **read_file**:首次读前 200 行,需要后续用 offset 翻页
- **search_code**:用正则搜坏味道模式(如 `except:` 裸异常、`for .* in .*:.*\\.(filter|get|execute)` N+1)
- **query_cve / run_semgrep**:本场景不用(安全专用工具)

## 输出规范
所有发现必须通过 submit_results 工具提交,每个 result 包含:
- title: 简短标题,如 "[correctness] 裸异常吞没 src/views.py:42"
- content: 问题描述 + 改进建议
- metadata: 必须包含以下字段:
    - issue_type: readability / correctness / performance / testing / architecture
    - file_path: 文件路径
    - line_range: 行号或范围,如 "42" 或 "42-45"
    - rule: 规则名,如 "bare-except" / "n-plus-1-query" / "long-function"
    - suggestion: 改进建议

## 注意
- 关注真实问题,不要凑数。小问题可合并到一条 result
- 测试代码里(tests/)的问题降级处理,重点看业务代码
- 禁止重复 read 同一个文件
- 单次审查控制在 20 轮以内
- 若无明显问题,也必须 submit_results(传空数组),并在最后一轮思考里说明已查范围
"""

    # ---------- 工具白名单 ----------

    @property
    def enabled_tools(self) -> list[str]:
        # 代码审查不需要 CVE/Semgrep(安全专用),只用通用代码浏览工具
        return [
            "clone_repo", "list_files", "read_file", "search_code",
            "list_skills", "skill",
        ]

    # ---------- submit_results 工具定义 ----------

    @property
    def submit_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "submit_results",
                "description": (
                    "提交本轮代码审查的所有结果。审查完成或确认无更多问题时必须调用。"
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
                                        "description": "场景专用信息(issue_type/file_path/line_range/rule/suggestion)",
                                    },
                                },
                                "required": ["title", "content"],
                            },
                        },
                        "summary": {
                            "type": "string",
                            "description": "本轮审查的总结说明(已查范围、结论)",
                        },
                    },
                    "required": ["results"],
                },
            },
        }

    # ---------- result 格式化 ----------

    def format_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把 LLM 提交的 raw result 转成数据库存储格式

        代码审查场景:LLM 应在 metadata 里放 issue_type/file_path/line_range/rule/suggestion
        这里做兜底:若 metadata 缺字段,补默认值
        """
        metadata = raw.get("metadata") or {}
        metadata.setdefault("issue_type", "readability")
        metadata.setdefault("file_path", None)
        metadata.setdefault("line_range", None)
        metadata.setdefault("rule", "")
        metadata.setdefault("suggestion", "")

        return {
            "title": raw.get("title", "(无标题)"),
            "content": raw.get("content", ""),
            "metadata": metadata,
        }

    # ---------- 前端声明(场景无关 UI 驱动) ----------

    @property
    def form_fields(self) -> list[dict[str, Any]]:
        """提交任务表单字段定义

        代码审查场景:与安全场景相同的字段(repo_url/branch/note),
        但 placeholder/label 体现审查语义(非安全)
        """
        return [
            {
                "name": "repo_url",
                "type": "url",
                "label": "GitHub 仓库地址",
                "required": True,
                "placeholder": "https://github.com/owner/repo",
                "description": "要审查的仓库地址",
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
                "placeholder": "如:重点关注 src/ 目录、只看最近一次提交",
            },
        ]

    @property
    def result_grouping(self) -> dict[str, Any]:
        """结果分组维度:按 issue_type 分组(非 severity)

        与安全场景对比:分组字段不同、枚举值不同、但结构相同,
        前端同一套代码即可渲染
        """
        return {
            "field": "issue_type",
            "type": "ordered",
            "values": [
                {"value": "correctness", "label": "正确性", "color": "critical", "order": 0},
                {"value": "performance", "label": "性能", "color": "high", "order": 1},
                {"value": "architecture", "label": "架构", "color": "medium", "order": 2},
                {"value": "readability", "label": "可读性", "color": "low", "order": 3},
                {"value": "testing", "label": "测试", "color": "info", "order": 4},
            ],
            "default_label": "其他",
            "default_color": "unknown",
        }

    @property
    def result_meta_fields(self) -> list[dict[str, Any]]:
        """结果 meta 字段展示:无 cwe,改为 rule + file_path(可点击)+ line_range

        与安全场景对比:无 cwe 字段,多了 rule 字段,但 file_path 仍是 file 类型可跳转
        """
        return [
            {"name": "rule", "label": "规则", "type": "text"},
            {"name": "file_path", "label": "文件", "type": "file"},
            {"name": "line_range", "label": "行号", "type": "text"},
        ]

    @property
    def coverage(self) -> dict[str, Any]:
        """覆盖度看板:维度派生自本场景 checklist 的 5 大维度

        与安全场景对比:维度数量和含义不同(5 个非安全维度 vs 7 个安全维度),
        但结构相同,前端同一套渲染逻辑
        """
        return {
            "dimensions": [
                {"id": c["id"], "name": c["name"], "description": c.get("description", "")}
                for c in self.checklist
            ],
        }


# 注册场景
_code_review = CodeReviewScenario()
register_scenario(_code_review)
