"""acp_base._build_prompt_message 单元测试(纯函数,不连 DB / 不连沙箱)。

覆盖 CLI 智能体 prompt 中项目记忆精简版 + 全局记忆的注入与拼接顺序。
"""
from unittest.mock import MagicMock

from app.agents.acp_base import _build_prompt_message


def _mk_task(user_input="审计这个仓库的安全问题", params=None):
    """构造最小 Task mock:_build_prompt_message 只用到 user_input / params。"""
    t = MagicMock()
    t.user_input = user_input
    t.params = params if params is not None else {"repo_url": "https://github.com/a/b"}
    return t


def test_first_round_includes_project_memory_and_file_hint():
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="## Hard Constraints\n- rule A",
    )
    assert "[项目记忆摘要]" in msg
    assert "## Hard Constraints\n- rule A" in msg
    assert "/home/user/.agent_memory/project_memory.md" in msg


def test_first_round_includes_global_memory():
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        global_memory="The following is general experience accumulated across tasks, organized by category (follow during execution):\n## Tech Stack\n- PG",
    )
    assert "The following is general experience accumulated across tasks" in msg
    assert "## Tech Stack\n- PG" in msg


def test_project_memory_before_global_memory():
    """项目记忆(具体)在前,全局记忆(通用)在后。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM_MARKER",
        global_memory="GLOBAL_MEM_MARKER",
    )
    assert msg.index("PROJECT_MEM_MARKER") < msg.index("GLOBAL_MEM_MARKER")


def test_empty_memories_not_appended():
    """memory_summary / global_memory 为空 → 不追加对应段。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="", global_memory="",
    )
    assert "[项目记忆摘要]" not in msg
    assert "项目记忆" not in msg


def test_followup_round_also_includes_memories():
    """追问轮同样注入记忆(与 react_agent system prompt 每轮都在一致)。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 2, "请重点检查 SQL 注入", None, "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM",
        global_memory="GLOBAL_MEM",
    )
    assert "[本轮追问]" in msg
    assert "请重点检查 SQL 注入" in msg
    assert "PROJECT_MEM" in msg
    assert "GLOBAL_MEM" in msg


def test_previous_plan_and_memories_coexist():
    """跨轮 plan 提醒 + 记忆段可共存,互不干扰。"""
    task = _mk_task()
    previous_plan = [{"text": "检查鉴权", "status": "done"}]
    msg = _build_prompt_message(
        task, 2, "继续检查", None, "/home/user/repos/r", previous_plan,
        memory_summary="PROJECT_MEM", global_memory="GLOBAL_MEM",
    )
    assert "PROJECT_MEM" in msg
    assert "GLOBAL_MEM" in msg
